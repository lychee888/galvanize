"""Cloud relay pull source (companion to relay/worker.js).

The daemon long-polls the user's Cloudflare Worker; each queued event is
dispatched through the emit trigger named by the ingest route. No inbound
ports, works behind any NAT, survives laptop sleep (worker holds the queue).

Auth: the relay's RELAY_TOKEN is stored in the OS
keyring under relay:token by `add webhook --relay` (the same value the
worker secret holds).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from ..events import Event
from ..relay_state import RelayState
from .. import state

logger = logging.getLogger("galvanize.relay")

POLL_TIMEOUT_S = 45      # worker long-poll isn't implemented; short poll is fine
MAX_BACKOFF_S = 300


class RelayWatcher:
    """One worker URL; dispatches every queued event to its emit trigger."""

    def __init__(self, url: str, token: str,
                 emit_by_route: Callable[[str, Event], None]) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.emit_by_route = emit_by_route
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.state = RelayState(self.url)
        self._since = self.state.cursor()
        self.last_error: Optional[str] = None
        self.connected = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="gz-relay")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _poll(self) -> Optional[dict]:
        q = urllib.parse.urlencode({"since": self.state.cursor('fetch'), "limit": 100})
        req = urllib.request.Request(f"{self.url}/events?{q}",
                                     headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=POLL_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def process_pending(self, now=None) -> None:
        for item in self.state.pending(now):
            event_id = item['id']
            try:
                ev = json.loads(item['event'])
                route = str(ev.get('route') or '')
                if not route:
                    self.state.finish(event_id, 'quarantined', 'Missing route', now=now)
                    continue
                payload = ev.get('body')
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except ValueError:
                        payload = {'raw': payload}
                if not isinstance(payload, dict):
                    payload = {'value': payload}
                # Stable id survives retries/restarts and is available to agents.
                payload = {**payload, 'event_id': event_id}
                result = self.emit_by_route(route, Event(
                    trigger_name=route, source='relay',
                    type=str(payload.get('event_type', 'relay.event')), payload=payload))
                # Legacy collector callbacks return None; the daemon returns a tuple.
                ok, detail = (True, '') if result is None else result
                if str(detail).startswith(('skipped: cooldown', 'skipped: dedupe in-flight')):
                    self.state.defer(event_id, now=now)
                    continue  # No delivery was attempted; keep the failure budget.
                self.state.finish(event_id, 'ack' if ok else 'pending', '' if ok else str(detail), now=now)
            except Exception as exc:
                self.state.finish(event_id, 'pending', f'{type(exc).__name__}: {exc}', now=now)
        self._since = self.state.cursor()

    def _loop(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            try:
                self.process_pending()
                body = self._poll()
                self.connected = True
                self.last_error = None
                backoff = 2.0
                body = body or {}
                events = body.get('events') or []
                self.state.stage(events, body.get('since'))
                state.set_source_health('relay', True)
                self.process_pending()
                self._stop.wait(2.0)
            except Exception as e:
                self.connected = False
                self.last_error = f"{type(e).__name__}: {e}"
                state.set_source_health('relay', False, self.last_error)
                logger.warning("relay: %s (retry in %.0fs)", self.last_error, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)
        self.connected = False
        state.set_source_health('relay', False, 'stopped')
