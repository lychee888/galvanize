"""Persistent per-trigger state: fires, errors, heartbeat.

state.json gives `galvanize status` its visibility-by-default numbers:
last-fire, last-error, fires-today, and the daemon heartbeat (so a dead
watcher shows up as a stale heartbeat instead of silent success).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional

from .paths import ensure_home, state_path


def _load() -> Dict[str, Any]:
    p = state_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(state: Dict[str, Any]) -> None:
    ensure_home()
    path = state_path()
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_fire(trigger_name: str, *, ok: bool, detail: str = "") -> None:
    state = _load()
    t = state.setdefault(trigger_name, {})
    now = time.time()
    if ok:
        t["last_fire"] = now
        t["last_error"] = None
        today = date.today().isoformat()
        if t.get("fires_date") != today:
            t["fires_date"] = today
            t["fires_today"] = 0
        t["fires_today"] = int(t.get("fires_today", 0)) + 1
    else:
        t["last_error"] = detail[:400]
        t["last_error_at"] = now
    _save(state)


def set_heartbeat() -> None:
    state = _load()
    state["_daemon"] = {"heartbeat": time.time(), "pid": os.getpid()}
    _save(state)


def clear_heartbeat() -> None:
    state = _load()
    if state.pop("_daemon", None) is not None:
        _save(state)


def get_heartbeat() -> Optional[dict]:
    return _load().get("_daemon")


def get(trigger_name: str) -> Dict[str, Any]:
    return _load().get(trigger_name, {})


def last_dispatch(trigger_name: str) -> Optional[str]:
    """Last successful dispatch response (for `test` feedback)."""
    return None


def _health_path(key: str) -> Path:
    import hashlib
    return ensure_home() / ('source-' + hashlib.sha256(key.encode()).hexdigest() + '.json')


def set_source_health(key: str, connected: bool, error: str = '') -> None:
    """Independent files avoid overwriting dispatch counters from watcher threads."""
    path = _health_path(key)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.health-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump({'connected': connected, 'error': error, 'ts': time.time(), 'pid': os.getpid()}, stream)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def source_health(key: str, daemon_pid=None) -> dict:
    try:
        health = json.loads(_health_path(key).read_text(encoding='utf-8'))
        if time.time() - health['ts'] > 60 or (daemon_pid is not None and health['pid'] != daemon_pid):
            return {'connected': False, 'error': 'stale watcher health'}
        return health
    except (OSError, ValueError, KeyError):
        return {'connected': False, 'error': 'watcher has not connected'}
