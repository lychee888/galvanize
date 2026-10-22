"""Persistent per-trigger run state: fires, errors, heartbeat, source health.

Card C5 (audit F8): every function here is a thin facade over the SQLite
authority (store.py). The old design read, mutated, and rewrote ONE whole
state.json per update — the main loop's heartbeat and a dispatcher thread's
record_fire raced with each other by construction, and a torn write lost
every counter at once. Heartbeat, per-trigger counters, and fire history are
separate rows now; each mutation is one short IMMEDIATE transaction.

`galvanize status` still gets its visibility-by-default numbers from here
(last-fire, last-error, fires-today, daemon heartbeat — so a dead watcher
shows up as a stale heartbeat instead of silent success).
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Any, Dict, Optional

from . import store


def _load() -> Dict[str, Any]:
    """Whole-file compatibility view (legacy callers). Prefer get()."""
    store.ensure_ready()
    out = store.all_trigger_state()
    hb = store.get_heartbeat()
    if hb:
        out["_daemon"] = hb
    return out


def record_fire(trigger_name: str, *, ok: bool, detail: str = "") -> None:
    store.record_fire(trigger_name, ok, detail)


def set_heartbeat() -> None:
    store.set_heartbeat(pid=os.getpid())


def clear_heartbeat() -> None:
    store.clear_heartbeat()


def get_heartbeat() -> Optional[dict]:
    store.ensure_ready()
    return store.get_heartbeat()


def live_daemon_pid(max_age_s: float = 60.0) -> Optional[int]:
    """PID of the daemon whose heartbeat is still fresh, else None."""
    hb = get_heartbeat()
    if not hb:
        return None
    try:
        if time.time() - float(hb.get("heartbeat", 0)) > max_age_s:
            return None
        return int(hb.get("pid") or 0) or None
    except (TypeError, ValueError):
        return None


def get(trigger_name: str) -> Dict[str, Any]:
    store.ensure_ready()
    return store.trigger_state(trigger_name)


# F14 (card C6): the pre-C5 `last_dispatch()` stub is gone — after config
# feedback moved onto store.fire_history(), no caller ever read the stub.


def source_health(key: str, daemon_pid=None) -> dict:
    try:
        store.ensure_ready()
        health = store.source_health_row(key)
        if health is None:
            # Pre-C5 migration stored legacy source-<hash>.json files under
            # their hashed name; fall back so freshly-migrated health still
            # reads until the watcher rewrites it under the plain key.
            health = store.source_health_row(
                "legacy:" + hashlib.sha256(key.encode()).hexdigest())
        if health is None:
            return {'connected': False, 'error': 'watcher has not connected'}
        age = time.time() - health['ts']
        if daemon_pid is not None:
            if health['pid'] != daemon_pid:
                # Written by a different (dead) daemon process: not current.
                return {'connected': False, 'error': 'stale watcher health'}
            # F3 (audit 2026-10-20): the writer process is the LIVE daemon —
            # its stored error is current truth even mid-backoff (a failed
            # imap watcher only rewrites health once per retry, up to 300s).
            # Keep the actionable error and annotate its age; do not mask it
            # with the generic 'stale watcher health'.
            if age > 60:
                err = health.get('error') or (
                    'connected' if health.get('connected')
                    else 'watcher not connected')
                health = dict(health, error=f"{err} ({int(age)}s ago)")
            return health
        if age > 60:
            return {'connected': False, 'error': 'stale watcher health'}
        return health
    except Exception:
        # A store that cannot be read is loud in doctor; one health lookup
        # must never take the status renderer down with it.
        return {'connected': False, 'error': 'state store unreadable'}


def set_source_health(key: str, connected: bool, error: str = '') -> None:
    """One UPSERT row per watcher — no whole-file rmw, no cross-thread loss."""
    store.set_source_health(key, connected, error)
