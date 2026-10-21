"""Shared fixture: isolate GALVANIZE_HOME and HERMES_HOME per test."""

import os
import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_homes(tmp_path, monkeypatch):
    ghome = tmp_path / "galvanize"
    hhome = tmp_path / "hermes"
    ghome.mkdir()
    hhome.mkdir()
    (hhome / "plugins").mkdir()
    monkeypatch.setenv("GALVANIZE_HOME", str(ghome))
    monkeypatch.setenv("HERMES_HOME", str(hhome))
    # Determinism: never read the real user's himalaya config in tests
    # (empty value = explicitly disabled; tests point it at a fixture).
    monkeypatch.setenv("GALVANIZE_HIMALAYA_CONFIG", "")
    yield {"galvanize": ghome, "hermes": hhome}


@pytest.fixture
def hermes_cfg(isolated_homes):
    """A hermes config.yaml with the webhook platform enabled on a test port."""
    import yaml

    cfg = {
        "platforms": {
            "webhook": {"enabled": True, "extra": {"port": 9999, "host": "127.0.0.1"}}
        }
    }
    p = isolated_homes["hermes"] / "config.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return cfg
