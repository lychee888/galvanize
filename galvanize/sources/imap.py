"""IMAP IDLE source — the push email trigger (imap_tools transport).

Reliability contract (PLAN A2; the hard 90%):
  - IDLE re-arm every IDLE_REARM_S (< 29 min; RFC 2177 advises re-issuing at
    29) — start/poll/stop cycle via imap_tools IdleManager
  - UID-based dedupe with persisted state {uidvalidity, last_uid, seen[]}
  - UIDVALIDITY change (server reshuffled UIDs): re-anchor at current max,
    never trust stale seen-sets across it
  - reconnect with exponential backoff; catch-up UID search after any gap
    (IDLE tells you nothing while the socket is dead)
  - xoauth2 support for Gmail; plain/app-password via secrets.py
  - first run anchors at current max: history is NEVER replayed at the agent

Only NEW mail (UID > anchor) fires; subject/from filters apply; a long
catch-up is capped per drain so it can't flood the dispatcher.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from imap_tools import MailBox, MailBoxUnencrypted, ImapToolsError

from ..events import Event
from .. import state as health_state
from ..paths import ensure_home, galvanize_home

logger = logging.getLogger("galvanize.imap")

IDLE_REARM_S = 1500  # 25 min — safely under RFC 2177's 29-minute advice
MAX_BACKOFF_S = 300
STATE_NAME = "imap_state.json"
FETCH_LIMIT = 50


def _state_path() -> Path:
    return galvanize_home() / STATE_NAME


class _StateStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {}
        p = _state_path()
        if p.exists():
            try:
                self._data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                self._data = {}

    def get(self, key: str) -> dict:
        with self._lock:
            return dict(self._data.get(key) or {})

    def put(self, key: str, value: dict) -> None:
        with self._lock:
            self._data[key] = value
            ensure_home()
            p = _state_path()
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            os.replace(tmp, p)


_STATE = _StateStore()


class ImapWatcher:
    """One account+folder watcher thread. Emits Event per qualifying new mail."""

    def __init__(self, trigger, emit: Callable[[Event], None],
                 password_provider: Optional[Callable[[], str]] = None) -> None:
        src = trigger.source
        self.name = trigger.name
        self.host = src["host"]
        self.user = src["user"]
        self.folder = str(src.get("folder", "INBOX"))
        self.port = int(src.get("port", 993))
        self.ssl = bool(src.get("ssl", True))
        self.auth = str(src.get("auth", "password"))
        self.patterns = list(src.get("patterns") or [])
        self._password = src.get("password", "")
        self._password_provider = password_provider
        self.filter_subject = str(src.get("filter_subject", ""))
        self.filter_from = str(src.get("filter_from", ""))
        self.emit = emit
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_error: Optional[str] = None
        self.connected = False

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name=f"gz-imap-{self.name}")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _get_password(self) -> str:
        if self._password_provider:
            p = self._password_provider()
            if p:
                return p
        return self._password

    def _open(self):
        cls = MailBox if self.ssl else MailBoxUnencrypted
        box = cls(self.host, self.port)
        pw = self._get_password()
        if self.auth == "xoauth2":
            box.xoauth2(self.user, pw)
        else:
            box.login(self.user, pw)
        box.folder.set(self.folder)
        return box

    # ---- main loop --------------------------------------------------------

    def _loop(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 2.0
            except Exception as e:
                self.connected = False
                self.last_error = f"{type(e).__name__}: {e}"
                health_state.set_source_health('imap:' + self.name, False, self.last_error)
                logger.warning("imap %s: %s (retry in %.0fs)",
                               self.name, self.last_error, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)
        self.connected = False
        health_state.set_source_health('imap:' + self.name, False, 'stopped')

    def _session(self) -> None:
        box = self._open()
        try:
            uidv = self._uidvalidity(box)
            state = _STATE.get(self.name)
            if state.get("uidvalidity") == uidv and isinstance(state.get("last_uid"), int):
                anchor = self._drain(box, state["last_uid"], emit=True)  # gap catch-up
            else:
                anchor = self._anchor(box, uidv)  # first run / reshuffle: no replay
            self.connected = True
            self.last_error = None
            health_state.set_source_health('imap:' + self.name, True)
            logger.info("imap %s: connected, anchor uid>%s (uidvalidity %s)",
                        self.name, anchor, uidv)
            self._idle_forever(box)
        finally:
            try:
                box.logout()
            except Exception:
                pass

    def _uidvalidity(self, box) -> str:
        try:
            status = box.folder.status(self.folder)
            if isinstance(status, dict):
                # imap_tools passes STATUS tokens through verbatim: uppercase
                return str(status.get("UIDVALIDITY",
                                      status.get("uidvalidity", 0)) or 0)
            return str(getattr(status, "uidvalidity", 0) or 0)
        except Exception:
            return "0"

    def _anchor(self, box, uidv: str) -> int:
        msgs = list(box.fetch(criteria="ALL", limit=1, reverse=True,
                              mark_seen=False, headers_only=True))
        try:
            anchor = int(msgs[0].uid) if msgs else 0
        except (TypeError, ValueError, IndexError):
            anchor = 0
        _STATE.put(self.name, {"uidvalidity": uidv, "last_uid": anchor,
                               "seen": [], "anchored_at": time.time()})
        return anchor

    # ---- IDLE (IdleManager start/poll/stop; re-arm under RFC 2177) ---------

    def _idle_forever(self, box) -> None:
        """Loop IDLE sessions; each session re-arms before the 29-min mark.

        Non-blocking poll(0) on a 1s tick: every loop iteration checks the
        re-arm deadline, so a busy mailbox can never starve the RFC 2177
        re-issue, and a new-message notification is acted on within ~1s.
        """
        anchor = _STATE.get(self.name).get("last_uid", 0)
        last_health = 0.0
        while not self._stop.is_set():
            box.idle.start()
            deadline = time.time() + IDLE_REARM_S
            try:
                while time.time() < deadline and not self._stop.is_set():
                    if time.time() - last_health >= 15:
                        health_state.set_source_health('imap:' + self.name, True)
                        last_health = time.time()
                    if box.idle.poll(timeout=0):
                        break  # DONE must precede SEARCH/FETCH on this connection.
                    self._stop.wait(1.0)
            finally:
                box.idle.stop()
            # session ended (re-arm or stop): catch-up search covers the gap
            anchor = self._drain(box, anchor, emit=True)

    # ---- mail handling ------------------------------------------------------

    def _drain(self, box, anchor: int, emit: bool = True) -> int:
        state = _STATE.get(self.name)
        seen = set(state.get("seen", []))
        try:
            msgs = list(box.fetch(criteria=f"UID {int(anchor) + 1}:*",
                                  limit=FETCH_LIMIT, mark_seen=False,
                                  headers_only=True))
        except ImapToolsError:
            raise
        except Exception as e:
            logger.warning("imap %s: fetch failed: %s", self.name, e)
            return anchor
        for m in msgs:
            try:
                uid = int(m.uid)
            except (TypeError, ValueError):
                continue
            if uid <= anchor or uid in seen:
                continue
            seen.add(uid)
            head = {"uid": uid,
                    "from": getattr(m.from_values, "email", None) or m.from_ or "",
                    "subject": m.subject or "", "date": str(m.date or ""),
                    "preview": (m.text or "")[:500]}
            if emit and self._qualifies(head):
                # UID-level dedupe is the persisted seen-set above; the bus
                # applies the trigger's own dedupe_key template (if any).
                self.emit(Event(
                    trigger_name=self.name, source="imap", type="mail.received",
                    payload=head))
            anchor = max(anchor, uid)
        if msgs or seen:
            _STATE.put(self.name, {**state, "last_uid": anchor,
                                   "seen": sorted(seen)[-2000:]})
        return anchor

    def _qualifies(self, head: dict) -> bool:
        import fnmatch
        subj = head.get("subject", "")
        frm = head.get("from", "")
        if self.filter_subject and self.filter_subject.lower() not in subj.lower():
            return False
        if self.filter_from and self.filter_from.lower() not in frm.lower():
            return False
        if self.patterns and not any(
                fnmatch.fnmatch(subj, p) or fnmatch.fnmatch(frm, p)
                for p in self.patterns):
            return False
        return True
