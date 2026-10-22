"""Filesystem locations.

GALVANIZE_HOME: state dir for galvanize itself (triggers.yaml, config.yaml,
state.json, logs). Default ~/.galvanize, override with GALVANIZE_HOME env.

hermes_home(): where Hermes keeps config.yaml and
webhook_subscriptions.json. Same resolution order Hermes itself uses
(hermes_constants.get_hermes_home): HERMES_HOME env -> %LOCALAPPDATA%\\hermes
on Windows -> ~/.hermes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def galvanize_home() -> Path:
    override = os.environ.get("GALVANIZE_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".galvanize"


def hermes_home() -> Path:
    override = os.environ.get("HERMES_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / "hermes"
    return Path.home() / ".hermes"


def triggers_path() -> Path:
    return galvanize_home() / "triggers.yaml"


def config_path() -> Path:
    return galvanize_home() / "config.yaml"


def state_path() -> Path:
    return galvanize_home() / "state.json"


def log_path() -> Path:
    return galvanize_home() / "galvanize.log"


def supervisor_log_path() -> Path:
    return galvanize_home() / "supervisor.log"


def supervisor_status_path() -> Path:
    """The supervisor's self-published state (single-instance authority
    snapshot; readers must check tick freshness, see supervisor.snapshot)."""
    return galvanize_home() / "supervisor.json"


def pause_flag_path() -> Path:
    """Watchdog pause marker (card C3): `daemon stop` touches it so the
    minute-cadence watchdog does not resurrect an intentionally-stopped
    daemon; the supervisor unlinks it at serve() start (intent-up)."""
    return galvanize_home() / "supervisor.pause"


def watchdog_log_path() -> Path:
    return galvanize_home() / "watchdog.log"


def ready_path() -> Path:
    """Worker -> supervisor authenticated READY post (token + pid + caps)."""
    return galvanize_home() / "ready.json"


def shutdown_flag_path() -> Path:
    return galvanize_home() / "supervisor.shutdown"


def claim_lock_path() -> Path:
    """POSIX stand-in for the Windows named mutex (same authority role)."""
    return galvanize_home() / "supervisor.lock"


def ensure_home() -> Path:
    home = galvanize_home()
    home.mkdir(parents=True, exist_ok=True)
    return home
