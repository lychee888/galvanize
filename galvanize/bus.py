"""The Trigger Bus: dedupe + cooldown, then dispatch.

Every source funnels normalized events through here so the guards live in
exactly one place:
  - dedupe_key: repeated events carrying the same key collapse to one wake
    (folder-watch double-fires, re-adding a file, source+lane overlap)
  - cooldown_s: minimum seconds between two wakes for one trigger
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional, Tuple

from . import dispatch as dispatch_mod
from .config import Trigger, load_triggers
from .events import Event
from .template import render

logger = logging.getLogger("galvanize.bus")


class TriggerBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_fire: Dict[str, float] = {}      # successful dispatches
        self._last_attempt: Dict[str, float] = {}   # for cooldown throttling
        self._seen_keys: Dict[str, Tuple[float, str]] = {}  # trigger -> (ts, key)

    def _gate(self, t: Trigger, event: Event) -> Tuple[Optional[str], Optional[str]]:
        """Return (skip_reason, pending_dedupe_key). skip_reason None -> fire."""
        now = time.time()
        pending: Optional[str] = None
        with self._lock:
            if t.dedupe_key:
                key = render(t.dedupe_key, event.payload)
                pending = key
                prev = self._seen_keys.get(t.name)
                if prev and prev[1] == key and (
                    now - prev[0] < 2.0
                ):
                    return f"dedupe '{key}'", None
            if t.cooldown_s:
                last = self._last_attempt.get(t.name, 0.0)
                if now - last < t.cooldown_s:
                    return f"cooldown ({int(now - last)}s < {int(t.cooldown_s)}s)", None
            self._last_attempt[t.name] = now
        return None, pending

    def handle(self, t: Trigger, event: Event) -> Tuple[bool, str]:
        """Handle an event for a trigger. Returns (ok_or_skipped, detail)."""
        if not t.enabled:
            return True, "trigger disabled"
        reason, pending_key = self._gate(t, event)
        if reason:
            logger.info("skip %s: %s", t.name, reason)
            return True, f"skipped: {reason}"
        prompt = render(t.prompt, event.payload)
        ok, detail = dispatch_mod.dispatch(t, event, prompt)
        if ok:
            with self._lock:
                self._last_fire[t.name] = time.time()
                if pending_key is not None:
                    self._seen_keys[t.name] = (time.time(), pending_key)
        else:
            logger.warning("dispatch failed for %s: %s", t.name, detail)
        return ok, detail

    def handle_named(self, trigger_name: str, event: Event) -> Tuple[bool, str]:
        ts = load_triggers()
        t = ts.get(trigger_name)
        if t is None:
            return False, f"unknown trigger '{trigger_name}'"
        return self.handle(t, event)
